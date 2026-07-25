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
# Idempotent and cheap on the satisfied path: every step is guarded by a presence check,
# so a re-run over a baseline that already has everything is a handful of local queries.
#
# Usage: ssh testuser@vm 'bash -s' < vm-test-fixtures.sh
#        (or: send the file and run `bash vm-test-fixtures.sh`)
set -euo pipefail

# Bumping this forces provisioning to rebuild the baseline: provision-test-infra.sh and
# run-integration-tests.sh compare the marker file's contents against their own copy of
# this number (PCSWITCHER_TEST_FIXTURES_VERSION in internal/common.sh — keep in sync).
readonly FIXTURES_VERSION=1
readonly MARKER=/etc/pcswitcher-test-fixtures

# Two snaps, because `test_system_refresh_hold_does_not_mask_a_per_snap_held_note` needs
# one snap held and a second one provably NOT held in the same `snap list` output.
#
# `hello` carries distinct revisions across its channels (stable/beta/edge), which is
# what lets the revision-divergence test move the target off the source's revision and
# watch the sync bring it back. `hello-world` is the canonical 20 kB demo snap. Both are
# strictly confined, contain no daemon, and are safe to hold, unhold, remove and
# reinstall; neither is a base snap anything else depends on.
readonly -a FIXTURE_SNAPS=(hello hello-world)

# A self-contained, GPG-signed OSTree repository served over file://, rather than
# Flathub: it is ~270 kB instead of ~270 MB (Flathub's smallest app still pulls
# org.freedesktop.Platform, 268 MB download / 688 MB installed), needs no network at
# test time, and exercises byte-for-byte the same `flatpak remote-add` / `flatpak
# install` path a real remote would.
#
# The public key is ALSO installed into ostree's system-wide trusted keyring. That is
# what makes the replicated remote usable on the target: pc-switcher replicates a remote
# as name+url (`flatpak remote-add --if-not-exists <name> <url>`), which produces a
# remote with GPG verification on and no key of its own, and `flatpak remote-delete`
# takes the per-remote keyring with it. A machine-level trust anchor is the normal way a
# fleet trusts its own repository, and it is a property of the machine rather than of
# the remote, so it survives the delete/re-add the flatpak test performs.
readonly FLATPAK_ROOT=/opt/pcswitcher-test-flatpak
readonly FLATPAK_REPO_DIR="${FLATPAK_ROOT}/repo"
readonly FLATPAK_PUBKEY="${FLATPAK_ROOT}/key.gpg"
readonly FLATPAK_GNUPGHOME="${FLATPAK_ROOT}/gnupg"
readonly OSTREE_TRUSTED_KEY=/usr/share/ostree/trusted.gpg.d/pcswitcher-test.gpg
readonly FLATPAK_REMOTE=pcswitcher-test
readonly FLATPAK_APP=org.pcswitcher.TestApp
readonly FLATPAK_APP_BRANCH=stable
readonly FLATPAK_RUNTIME=org.pcswitcher.TestRuntime
readonly FLATPAK_RUNTIME_BRANCH=1
readonly FLATPAK_ARCH=x86_64

log() { echo "[vm-test-fixtures] $*"; }

# -- snaps -------------------------------------------------------------------------

install_snaps() {
    if ! command -v snap >/dev/null 2>&1; then
        log "installing snapd"
        sudo apt-get update
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y snapd
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
    local -a missing=()
    command -v flatpak >/dev/null 2>&1 || missing+=(flatpak)
    command -v gpg >/dev/null 2>&1 || missing+=(gnupg)
    if [[ ${#missing[@]} -eq 0 ]]; then
        log "flatpak and gnupg already installed"
        return
    fi
    log "installing apt packages: ${missing[*]}"
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

build_flatpak_repo() {
    if [[ -d "$FLATPAK_REPO_DIR" && -f "$FLATPAK_PUBKEY" && -f "$OSTREE_TRUSTED_KEY" ]]; then
        log "flatpak fixture repository already built"
        return
    fi

    log "building signed flatpak fixture repository at $FLATPAK_REPO_DIR"
    # One root shell for the whole build: every path written here is root-owned.
    # Variables are expanded by THIS shell (unquoted heredoc delimiter) so the remote
    # script needs no argument plumbing.
    sudo bash -s <<EOF
set -euo pipefail

# sudo leaves HOME pointing at the invoking user on Ubuntu; without this, root-run
# flatpak and gpg would drop root-owned files into the test user's ~/.cache and break
# that user's own flatpak later.
export HOME=/root

rm -rf "${FLATPAK_ROOT}"
mkdir -p "${FLATPAK_GNUPGHOME}" "${FLATPAK_REPO_DIR}"
chmod 700 "${FLATPAK_GNUPGHOME}"
export GNUPGHOME="${FLATPAK_GNUPGHOME}"

# Passphrase-less signing key, generated per machine: each VM verifies against its own
# local repository, so the two machines need no shared key material.
gpg --batch --quiet --passphrase '' --quick-gen-key \
    'pc-switcher integration test repo <test@pcswitcher.invalid>' default default never
KEY_FINGERPRINT=\$(gpg --batch --with-colons --list-keys | awk -F: '/^fpr:/{print \$10; exit}')  # codespell:ignore fpr
if [[ -z "\$KEY_FINGERPRINT" ]]; then
    echo "failed to generate a signing key for the flatpak fixture repository" >&2
    exit 1
fi

# A runtime tree flatpak accepts for export needs metadata, usr/ (its payload), and the
# empty files/ and export/ directories `flatpak build-init` would have created.
RT="${FLATPAK_ROOT}/build/runtime"
mkdir -p "\$RT/usr/bin" "\$RT/files" "\$RT/export"
cat > "\$RT/metadata" <<'META'
[Runtime]
name=${FLATPAK_RUNTIME}
META
printf '#!/bin/sh\nexit 0\n' > "\$RT/usr/bin/pcsw-runtime-noop"
chmod +x "\$RT/usr/bin/pcsw-runtime-noop"

APP="${FLATPAK_ROOT}/build/app"
mkdir -p "\$APP/files/bin" "\$APP/export"
cat > "\$APP/metadata" <<'META'
[Application]
name=${FLATPAK_APP}
runtime=${FLATPAK_RUNTIME}/${FLATPAK_ARCH}/${FLATPAK_RUNTIME_BRANCH}
sdk=${FLATPAK_RUNTIME}/${FLATPAK_ARCH}/${FLATPAK_RUNTIME_BRANCH}
command=pcsw-hello
META
printf '#!/bin/sh\necho "hello from the pc-switcher integration fixture"\n' > "\$APP/files/bin/pcsw-hello"
chmod +x "\$APP/files/bin/pcsw-hello"

flatpak build-export --runtime --arch="${FLATPAK_ARCH}" \
    --gpg-sign="\$KEY_FINGERPRINT" --gpg-homedir="${FLATPAK_GNUPGHOME}" \
    "${FLATPAK_REPO_DIR}" "\$RT" "${FLATPAK_RUNTIME_BRANCH}"
flatpak build-export --arch="${FLATPAK_ARCH}" \
    --gpg-sign="\$KEY_FINGERPRINT" --gpg-homedir="${FLATPAK_GNUPGHOME}" \
    "${FLATPAK_REPO_DIR}" "\$APP" "${FLATPAK_APP_BRANCH}"
flatpak build-update-repo --gpg-sign="\$KEY_FINGERPRINT" --gpg-homedir="${FLATPAK_GNUPGHOME}" "${FLATPAK_REPO_DIR}"

gpg --batch --export "\$KEY_FINGERPRINT" > "${FLATPAK_PUBKEY}"
install -d -m 755 "\$(dirname "${OSTREE_TRUSTED_KEY}")"
install -m 644 "${FLATPAK_PUBKEY}" "${OSTREE_TRUSTED_KEY}"

# The repository is served over file:// to an unprivileged user.
chmod 644 "${FLATPAK_PUBKEY}"
chmod -R a+rX "${FLATPAK_REPO_DIR}"
chmod 755 "${FLATPAK_ROOT}"
EOF
}

install_flatpak_app() {
    if ! flatpak remotes --user --columns=name | grep -qx "$FLATPAK_REMOTE"; then
        log "adding user-scope flatpak remote $FLATPAK_REMOTE"
        flatpak remote-add --user --gpg-import="$FLATPAK_PUBKEY" \
            "$FLATPAK_REMOTE" "file://${FLATPAK_REPO_DIR}"
    else
        log "flatpak remote $FLATPAK_REMOTE already configured"
    fi

    if ! flatpak list --user --app --columns=application | grep -qx "$FLATPAK_APP"; then
        log "installing user-scope flatpak $FLATPAK_APP"
        flatpak install --user -y --noninteractive "$FLATPAK_REMOTE" "$FLATPAK_APP"
    else
        log "flatpak $FLATPAK_APP already installed"
    fi
}

# -- main --------------------------------------------------------------------------

# A marker from an older version means the fixtures on this machine are not the ones
# this script now describes. Drop the built artifacts so they are rebuilt rather than
# skipped by the presence checks below (snaps need no such reset — they are checked
# name by name, so a newly added one is installed and an existing one left alone).
if [[ -f "$MARKER" ]] && [[ "$(cat "$MARKER")" != "$FIXTURES_VERSION" ]]; then
    log "marker reports version $(cat "$MARKER"), rebuilding for version $FIXTURES_VERSION"
    flatpak uninstall --user -y "$FLATPAK_APP" >/dev/null 2>&1 || true
    flatpak remote-delete --user --force "$FLATPAK_REMOTE" >/dev/null 2>&1 || true
    sudo rm -rf "$FLATPAK_ROOT" "$OSTREE_TRUSTED_KEY"
fi

install_snaps
install_flatpak_packages
build_flatpak_repo
install_flatpak_app

printf '%s\n' "$FIXTURES_VERSION" | sudo tee "$MARKER" >/dev/null
log "test fixtures ready (version $FIXTURES_VERSION)"
