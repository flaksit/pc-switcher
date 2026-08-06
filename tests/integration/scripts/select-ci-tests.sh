#!/usr/bin/env bash
# Select which integration tests CI should run for a PR, based on the files it
# changes. Topic-scoped changes run their area's tests plus the smoke set;
# anything outside the mapped areas selects the full suite.
#
# Usage: select-ci-tests.sh <base-ref>
#
#   base-ref  Git ref to diff HEAD against (e.g. origin/main). The diff uses the
#             merge base (triple-dot), so only the PR's own changes count.
#
# Output (stdout):
#   - A pytest -m expression such as "smoke or area_package" → run that selection.
#   - The word "full" → run the full suite.
# Classification rationale goes to stderr so workflow logs show why.
#
# Two mappings feed the expression:
#   - Source files → area, via the case patterns below. Every file must match,
#     or the whole suite runs: new source files run the full suite until someone
#     maps them here — silently running too much, never too little.
#   - Changed test files → areas, read from the file's own smoke/area_* markers
#     (a file may carry several; presence of at least one on every test is
#     enforced at collection time in tests/integration/conftest.py).
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

full() {
    echo "'$1' -> full suite" >&2
    echo "full"
    exit 0
}

# Smoke (connectivity, version resolution, config sync) is part of every
# topic-scoped selection as cheap cross-cutting sanity.
areas="smoke"

add_area() {
    case " $areas " in
        *" $1 "*) ;;
        *) areas="$areas $1" ;;
    esac
}

while IFS= read -r f; do
    case "$f" in
        # Files that never influence integration behavior. The workflow's outer
        # paths filter usually catches these already; matched here too so a PR
        # mixing docs with a topic change stays topic-scoped. Unit tests are
        # gated by ci.yml, benchmarks are deselected from CI integration runs.
        docs/* | *.md | .planning/* | LICENSE | tests/unit/* | tests/integration/benchmarks/*)
            ;;
        # A changed integration test runs its own areas: read them from the file's
        # markers. Every smoke/area_* marker in the file counts, wherever it sits
        # (module pytestmark, class pytestmark, per-test decorator), so a file
        # spanning areas selects all of them. Deleted files (absent from the
        # checkout) and files without a recognizable marker fall back to the full
        # suite.
        tests/integration/*test_*.py)
            markers=$([[ -f "$f" ]] && grep --only-matching --extended-regexp 'pytest\.mark\.(area_[a-z]+|smoke)' "$f" | cut --delimiter='.' --fields=3 | sort --unique || true)
            if [[ -z "$markers" ]]; then
                full "$f (no smoke/area_* marker found)"
            fi
            while IFS= read -r marker; do
                add_area "$marker"
            done <<< "$markers"
            ;;
        # package_sync_scenario.py is a helper, not a test file: it carries no markers of
        # its own, so it is mapped here like a source file. A test module outside
        # area_package that imports it must add its own area to this branch.
        src/pcswitcher/jobs/apt_sync/* | src/pcswitcher/jobs/snap_sync.py | \
        src/pcswitcher/jobs/flatpak_sync.py | src/pcswitcher/jobs/manual_deb_sync.py | \
        src/pcswitcher/jobs/manual_snap_sync.py | src/pcswitcher/jobs/manual_flatpak_sync.py | \
        src/pcswitcher/jobs/manual_installs_sync.py | \
        src/pcswitcher/jobs/packages/* | tests/integration/jobs/package_sync_scenario.py)
            add_area area_package
            ;;
        install.sh | src/pcswitcher/install.py | src/pcswitcher/version.py | \
        src/pcswitcher/jobs/install_on_target.py)
            add_area area_install
            ;;
        src/pcswitcher/btrfs_snapshots.py | src/pcswitcher/jobs/btrfs.py)
            add_area area_btrfs
            ;;
        # folder_sync_scenario.py is a helper, not a test file: it carries no markers of
        # its own, so it is mapped here like a source file.
        src/pcswitcher/jobs/folder_sync.py | src/pcswitcher/home.filter | src/pcswitcher/root.filter | \
        tests/integration/jobs/folder_sync_scenario.py)
            add_area area_folder
            ;;
        # Core modules the area_core tests own outright: the orchestrator is their only
        # importer, and the tests that assert their behavior (test_lock_integration.py,
        # test_sync_order_gates.py) carry area_core. The rest of the spine — orchestrator.py,
        # cli.py, config.py, executor.py, connection.py, logger.py — reaches every area's
        # tests and so stays unmapped below, selecting the full suite.
        src/pcswitcher/lock.py | src/pcswitcher/sync_history.py)
            add_area area_core
            ;;
        *)
            full "$f (outside every mapped area)"
            ;;
    esac
done <<< "$changed"

echo "Topic-scoped selection: $areas" >&2
echo "${areas// / or }"
