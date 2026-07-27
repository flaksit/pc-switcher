#!/usr/bin/env bash
# Select which integration test paths CI should run for a PR, based on the files
# it changes. Topic-scoped changes run their area's tests plus a small smoke set;
# anything outside the mapped areas selects the full suite.
#
# Usage: select-ci-tests.sh <base-ref>
#
#   base-ref  Git ref to diff HEAD against (e.g. origin/main). The diff uses the
#             merge base (triple-dot), so only the PR's own changes count.
#
# Output (stdout):
#   - A space-separated list of pytest paths → run exactly these.
#   - The word "full" → run the full suite.
# Classification rationale goes to stderr so workflow logs show why.
#
# The area→tests mapping errs toward "full": every file must match an area
# pattern, or the whole suite runs. New source files therefore run the full
# suite until someone maps them here — silently running too much, never too
# little.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <base-ref>" >&2
    exit 2
fi
base="$1"

changed=$(git diff --name-only "$base"...HEAD)

if [[ -z "$changed" ]]; then
    echo "No changed files detected against $base; defaulting to full suite" >&2
    echo "full"
    exit 0
fi

# Always included: cheap sanity that connectivity, versioning, and the config
# sync path still work, regardless of which area a PR touches.
SMOKE_TESTS="tests/integration/test_vm_connectivity.py tests/integration/test_version_resolution.py tests/integration/test_config_sync.py"

PACKAGE_TESTS="tests/integration/jobs/test_package_sync.py"
INSTALL_TESTS="tests/integration/test_self_update.py tests/integration/test_installation_script.py tests/integration/jobs/test_install_on_target_job.py"
BTRFS_TESTS="tests/integration/test_snapshot_infrastructure.py tests/integration/test_btrfs_operations.py"
FOLDER_TESTS="tests/integration/test_end_to_end_sync.py"

need_package=false
need_install=false
need_btrfs=false
need_folder=false

while IFS= read -r f; do
    case "$f" in
        # Files that never influence integration behavior. The workflow's outer
        # paths filter usually catches these already; matched here too so a PR
        # mixing docs with a topic change stays topic-scoped. Unit tests are
        # gated by ci.yml, benchmarks are deselected from CI integration runs.
        docs/* | *.md | .planning/* | LICENSE | tests/unit/* | tests/integration/benchmarks/*)
            ;;
        src/pcswitcher/jobs/apt_sync.py | src/pcswitcher/jobs/snap_sync.py | \
        src/pcswitcher/jobs/flatpak_sync.py | src/pcswitcher/jobs/manual_installs_sync.py | \
        src/pcswitcher/jobs/packages/* | src/pcswitcher/machine-packages.example.yaml | \
        tests/integration/jobs/test_package_sync.py)
            need_package=true
            ;;
        install.sh | src/pcswitcher/install.py | src/pcswitcher/version.py | \
        src/pcswitcher/jobs/install_on_target.py | \
        tests/integration/test_self_update.py | tests/integration/test_installation_script.py | \
        tests/integration/jobs/test_install_on_target_job.py)
            need_install=true
            ;;
        src/pcswitcher/btrfs_snapshots.py | src/pcswitcher/jobs/btrfs.py | \
        tests/integration/test_snapshot_infrastructure.py | tests/integration/test_btrfs_operations.py)
            need_btrfs=true
            ;;
        src/pcswitcher/jobs/folder_sync.py | src/pcswitcher/home.filter | src/pcswitcher/root.filter | \
        tests/integration/test_end_to_end_sync.py)
            need_folder=true
            ;;
        *)
            echo "'$f' is outside every mapped area -> full suite" >&2
            echo "full"
            exit 0
            ;;
    esac
done <<< "$changed"

selected="$SMOKE_TESTS"
$need_package && selected="$selected $PACKAGE_TESTS"
$need_install && selected="$selected $INSTALL_TESTS"
$need_btrfs && selected="$selected $BTRFS_TESTS"
$need_folder && selected="$selected $FOLDER_TESTS"

echo "Topic-scoped selection (package=$need_package install=$need_install btrfs=$need_btrfs folder=$need_folder)" >&2
echo "$selected"
